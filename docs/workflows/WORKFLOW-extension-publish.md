# WORKFLOW: Extension Publishing Pipeline

**Version**: 0.1
**Date**: 2026-03-29
**Author**: Workflow Architect
**Status**: Draft
**Implements**: Task 340c — Extension Publish Pipeline + UX Polish

---

## Overview

This workflow publishes the Bernstein VS Code extension to both the VS Code Marketplace and Open VSX (for Cursor, VSCodium, Gitpod). It includes account setup, credential management, CI/CD validation, publishing, and community verification. The workflow is triggered by a git tag and produces multi-channel distribution.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Developer | Creates git tag, triggers the workflow |
| GitHub Actions | Validates, builds, publishes to marketplaces |
| VS Code Marketplace | Official VS Code extension registry |
| Open VSX | Community extension registry (Cursor, VSCodium, Gitpod) |
| Extension user | Installs from marketplace or manual VSIX |
| Cursor community | Verifies extension works in Cursor, confirms publisher |

---

## Prerequisites

- **Publisher accounts created and verified**:
  - VS Code Marketplace publisher account (ID: `chernistry`) at marketplace.visualstudio.com
  - Open VSX account with Eclipse identity
  - Both accounts with verified email
- **Credentials stored as GitHub secrets**:
  - `VS_MARKETPLACE_TOKEN` — Azure DevOps PAT with "Marketplace > Manage" scope
  - `OPEN_VSX_TOKEN` — Open VSX access token from account settings
- **Extension files exist and validated**:
  - `packages/vscode/package.json` with correct publisher ID and version
  - `packages/vscode/media/bernstein-icon.png` (≥128x128 PNG)
  - `packages/vscode/README.md` (shown on marketplace)
  - `packages/vscode/CHANGELOG.md` (shown on marketplace)
  - `packages/vscode/media/screenshots/` with at least 3 images
- **Build pipeline working**:
  - `npm ci` succeeds in packages/vscode/
  - `tsc --noEmit` passes (no type errors)
  - `npm test` passes (all tests pass)
  - `npm run compile` produces dist/extension.js

---

## Trigger

**User action**: Git tag creation and push

```bash
cd /path/to/bernstein
git tag ext-v0.1.0 -m "Release VS Code extension v0.1.0"
git push origin ext-v0.1.0
```

This triggers `.github/workflows/publish-extension.yml` because the tag matches pattern `ext-v*`.

**Webhook**: GitHub Actions automatically detects the push and triggers the workflow.

---

## Workflow Tree

### STEP 1: Checkout code and setup environment
**Actor**: GitHub Actions
**Action**: Clone repo, setup Node.js 20, configure npm cache
**Timeout**: 30s
**Input**: Git tag ref (e.g., `ext-v0.1.0`)
**Output on SUCCESS**:
  - Node 20 installed
  - Code checked out at the tagged commit
  - npm cache configured
  - GO TO STEP 2
**Output on FAILURE**:
  - `FAILURE(setup_error)`: Node installation failed or cache misconfigured → [recovery: retry entire workflow with same tag]

**Observable states during this step**:
  - Customer sees: Nothing yet (workflow running silently)
  - Operator sees: Workflow execution log in GitHub Actions
  - Logs: `[GitHub Actions] Node.js v20.x installed`, `[GitHub Actions] Checked out ref: ext-v0.1.0`

---

### STEP 2: Validate extension icon
**Actor**: GitHub Actions
**Action**: Read `packages/vscode/media/bernstein-icon.png`, validate PNG signature, check dimensions ≥128x128
**Timeout**: 10s
**Input**: File path: `packages/vscode/media/bernstein-icon.png`
**Output on SUCCESS**:
  - Icon is valid PNG
  - Dimensions logged (e.g., "1024x1024")
  - GO TO STEP 3
**Output on FAILURE**:
  - `FAILURE(invalid_icon_format)`: File is not a valid PNG → [recovery: ABORT_CLEANUP — workflow fails, user must fix icon and retag]
  - `FAILURE(icon_too_small)`: Dimensions < 128x128 → [recovery: ABORT_CLEANUP — workflow fails, user must fix icon and retag]

**Observable states during this step**:
  - Operator sees: Validation step in workflow log
  - Logs: `[Step 2] Icon dimensions: 1024x1024` or `[Step 2] ERROR: Icon must be at least 128x128 pixels`

---

### STEP 3: Install dependencies
**Actor**: GitHub Actions (npm)
**Action**: `npm ci` in `packages/vscode/` (clean install using package-lock.json)
**Timeout**: 60s
**Input**: `package-lock.json` (exact dependency list)
**Output on SUCCESS**:
  - `node_modules/` populated
  - All devDependencies installed
  - GO TO STEP 4
**Output on FAILURE**:
  - `FAILURE(npm_error)`: Package installation failed (network, registry unavailable, corrupted package-lock) → [recovery: retry up to 2 times with 10s backoff, then ABORT_CLEANUP]
  - `FAILURE(version_conflict)`: package-lock.json out of sync with package.json → [recovery: ABORT_CLEANUP — user must update locks and retag]

**Observable states during this step**:
  - Logs: `npm WARN`, `npm ERR!` (if errors), final `added X packages`

---

### STEP 4: Type check
**Actor**: GitHub Actions (TypeScript compiler)
**Action**: `npx tsc --noEmit` in `packages/vscode/` (verify no type errors)
**Timeout**: 30s
**Input**: TypeScript source files in `src/`
**Output on SUCCESS**:
  - Zero type errors
  - GO TO STEP 5
**Output on FAILURE**:
  - `FAILURE(type_error)`: TypeScript compilation failed (syntax, missing types, type mismatch) → [recovery: ABORT_CLEANUP — user must fix types and retag]

**Observable states during this step**:
  - Logs: `error TS####: [message]` (if errors), or silent (if success)

---

### STEP 5: Run tests
**Actor**: GitHub Actions (Jest)
**Action**: `npm test` in `packages/vscode/` (execute Jest test suite)
**Timeout**: 60s
**Input**: Test files in `src/__tests__/`
**Output on SUCCESS**:
  - All tests pass
  - Coverage report generated (if configured)
  - GO TO STEP 6
**Output on FAILURE**:
  - `FAILURE(test_failure)`: One or more tests failed → [recovery: ABORT_CLEANUP — user must fix failing tests and retag]

**Observable states during this step**:
  - Logs: `PASS src/__tests__/foo.test.ts` or `FAIL src/__tests__/foo.test.ts`

---

### STEP 6: Build extension
**Actor**: GitHub Actions (esbuild)
**Action**: `npm run compile` (bundle TypeScript to `dist/extension.js`)
**Timeout**: 30s
**Input**: TypeScript source + all dependencies
**Output on SUCCESS**:
  - `dist/extension.js` produced (bundled, minified)
  - dist/ directory ready for packaging
  - GO TO STEP 7
**Output on FAILURE**:
  - `FAILURE(build_error)`: esbuild failed (missing file, syntax error, import error) → [recovery: ABORT_CLEANUP — user must fix build errors and retag]

**Observable states during this step**:
  - Logs: `[build] dist/extension.js [size: 123KB]` or build errors

---

### STEP 7: Package extension
**Actor**: GitHub Actions (vsce)
**Action**: `vsce package --no-dependencies` (create .vsix file)
**Timeout**: 30s
**Input**: `dist/extension.js`, `package.json`, README.md, CHANGELOG.md, media/
**Output on SUCCESS**:
  - `packages/vscode/bernstein-*.vsix` file created (e.g., bernstein-0.1.0.vsix)
  - Size typically 200-500KB
  - GO TO STEP 8
**Output on FAILURE**:
  - `FAILURE(vsce_error)`: vsce packaging failed (missing icon, README, bad manifest) → [recovery: ABORT_CLEANUP]

**Observable states during this step**:
  - Logs: `VSIX created` or vsce error messages

---

### STEP 8: Publish to VS Code Marketplace
**Actor**: GitHub Actions (HaaLeo/publish-vscode-extension action)
**Action**: Upload .vsix to VS Code Marketplace using `VS_MARKETPLACE_TOKEN`
**Timeout**: 60s
**Input**:
  - .vsix file path
  - PAT (from GitHub secret)
  - Marketplace registry URL
**Output on SUCCESS**:
  - Extension published at marketplace.visualstudio.com/items?itemName=chernistry.bernstein
  - Searchable in VS Code extensions within ~5 minutes
  - GO TO STEP 9
**Output on FAILURE**:
  - `FAILURE(marketplace_auth)`: Invalid or expired token → [recovery: ABORT_CLEANUP — user must refresh VS_MARKETPLACE_TOKEN secret]
  - `FAILURE(marketplace_conflict)`: Version already published → [recovery: ABORT_CLEANUP — user must bump version and retag]
  - `FAILURE(marketplace_upload)`: Upload failed (network, server error) → [recovery: retry 2x with 15s backoff, then ABORT_CLEANUP]

**Observable states during this step**:
  - Logs: `Publishing to VS Code Marketplace...` or `ERROR: ...`
  - User can search marketplace immediately after success (may take 5-10 min to appear in search)

---

### STEP 9: Publish to Open VSX
**Actor**: GitHub Actions (HaaLeo/publish-vscode-extension action)
**Action**: Upload .vsix to Open VSX using `OPEN_VSX_TOKEN`
**Timeout**: 60s
**Input**:
  - .vsix file path
  - Access token (from GitHub secret)
  - Open VSX registry URL
**Output on SUCCESS**:
  - Extension published at open-vsx.org/extension/chernistry/bernstein
  - Searchable in Cursor extensions within ~5 minutes
  - GO TO STEP 10
**Output on FAILURE**:
  - `FAILURE(ovsx_auth)`: Invalid or expired token → [recovery: ABORT_CLEANUP — user must refresh OPEN_VSX_TOKEN]
  - `FAILURE(ovsx_namespace)`: Namespace doesn't exist or publisher mismatch → [recovery: ABORT_CLEANUP — user must verify namespace on open-vsx.org]
  - `FAILURE(ovsx_upload)`: Upload failed → [recovery: retry 2x with 15s backoff, then ABORT_CLEANUP]

**Observable states during this step**:
  - Logs: `Publishing to Open VSX...` or `ERROR: ...`
  - Users with Cursor can find extension within ~5-10 min

---

### STEP 10: Create GitHub Release
**Actor**: GitHub Actions (softprops/action-gh-release)
**Action**: Create GitHub Release with tag, upload .vsix as asset, write release notes
**Timeout**: 30s
**Input**:
  - Git tag (e.g., `ext-v0.1.0`)
  - .vsix file
  - Release body (markdown with install instructions)
**Output on SUCCESS**:
  - GitHub Release created at github.com/chernistry/bernstein/releases/tag/ext-v0.1.0
  - .vsix attached as downloadable asset
  - Release notes visible (3 install options listed)
  - GO TO STEP 11
**Output on FAILURE**:
  - `FAILURE(release_conflict)`: Release for this tag already exists → [recovery: ABORT_CLEANUP — workflow failed but marketplaces already have version]
  - `FAILURE(github_auth)`: Missing write permissions → [recovery: ABORT_CLEANUP — workflow permissions issue]

**Observable states during this step**:
  - Logs: `Creating release: ext-v0.1.0` or error messages
  - User can download .vsix directly from releases page immediately

---

### STEP 11: Mark workflow as complete
**Actor**: GitHub Actions
**Action**: Workflow completes successfully, all steps passed
**Output**: SUCCESS — Extension now available on:
  - VS Code Marketplace (searchable in ~5-10 min)
  - Open VSX / Cursor (searchable in ~5-10 min)
  - GitHub Releases (downloadable immediately as .vsix)

---

## Cleanup Inventory

This workflow has no rollback capability once a version is published to marketplaces (they don't support deletion by publishers, only by the registry admins).

| Resource | Created at step | Destroyed by | Recovery |
|---|---|---|---|
| GitHub Release | Step 10 | Manual (GitHub only, via web UI) | If workflow succeeds but extension has critical bug, next publish must bump version |
| .vsix artifact | Step 7 | Automatic (CI runner cleanup) | Fallback: re-run workflow with same tag (will re-upload to marketplaces) |

---

## ABORT_CLEANUP

**Triggered by**: Any step fails and is not retryable, or retry limit exceeded

**Actions** (in order):
  1. Workflow marked as FAILED in GitHub Actions UI
  2. Error message logged with specific failure reason
  3. Marketplaces: No changes (no version was published or partial publish occurred)
  4. GitHub Release: Not created (Step 10 not reached unless earlier step failed)

**What developer sees**:
  - Red X on workflow run in github.com/chernistry/bernstein/actions
  - Error message in log describing exact failure point
  - No extension published to any marketplace

**What operator sees** (if monitoring):
  - Workflow run status: FAILED
  - Can re-run or push new tag to retry

---

## State Transitions

```
[waiting_for_tag]
  → (developer pushes tag ext-v*)
  → [workflow_triggered]
    → (Steps 1-7 all pass)
    → [ready_to_publish]
      → (Steps 8-9 succeed)
      → [published_to_marketplaces]
        → (Step 10 succeeds)
        → [released] ✓
    → (any step fails)
    → [failed] ✗
```

---

## Handoff Contracts

### GitHub Actions → VS Code Marketplace
**Endpoint**: `POST https://marketplace.visualstudio.com/api/publishers/{publisher}/extensions/{extension}/versions`
**Auth**: Bearer token (VS_MARKETPLACE_TOKEN)
**Payload**:
```json
{
  "vsix": "[binary VSIX file]",
  "metadata": {
    "assetUri": "https://github.com/chernistry/bernstein/releases/tag/ext-v0.1.0",
    "fallbackAssetUri": null
  }
}
```
**Success response**:
```json
{
  "id": "12345",
  "version": "0.1.0",
  "flags": "validated",
  "publishedDate": "2026-03-29T...",
  "releaseNotes": null
}
```
**Failure response**:
```json
{
  "code": "GalleryNotAuthorized",
  "message": "The Personal Access Token does not have the necessary scope to perform this action"
}
```
**Timeout**: 60s — treated as FAILURE, retryable with backoff

---

### GitHub Actions → Open VSX
**Endpoint**: `POST https://open-vsx.org/api/publish`
**Auth**: Bearer token (OPEN_VSX_TOKEN)
**Payload**:
```
[multipart/form-data]
file: [binary VSIX]
```
**Success response**:
```json
{
  "success": true,
  "publishedVersion": "0.1.0",
  "publishedDate": "2026-03-29T..."
}
```
**Failure response**:
```json
{
  "error": "Invalid personal access token"
}
```
**Timeout**: 60s — treated as FAILURE, retryable

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path | Push tag `ext-v0.1.0` with all assets valid | All 10 steps pass, extension available on both marketplaces within 5-10 min |
| TC-02: Invalid icon format | Push tag with non-PNG icon | Step 2 fails, workflow aborts, no marketplace changes |
| TC-03: Icon too small | Push tag with 64x64 icon | Step 2 fails with dimension error, workflow aborts |
| TC-04: Type errors in source | Push tag with TypeScript error | Step 4 fails, workflow aborts, user must fix and retag |
| TC-05: Test failure | Push tag with failing test | Step 5 fails, workflow aborts |
| TC-06: Build failure | Push tag with broken esbuild config | Step 6 fails, workflow aborts |
| TC-07: Missing VS_MARKETPLACE_TOKEN | Push tag without secret configured | Step 8 skipped (if condition false), Step 9 still runs, extension only on Open VSX |
| TC-08: Missing OPEN_VSX_TOKEN | Push tag without secret configured | Step 9 skipped (if condition false), Step 8 still runs, extension only on VS Code Marketplace |
| TC-09: Expired VS_MARKETPLACE_TOKEN | Push tag with revoked token | Step 8 fails with auth error, workflow aborts, marketplace not updated |
| TC-10: Version already published | Push tag matching existing version on marketplace | Step 8 fails with conflict error, workflow aborts |
| TC-11: Network timeout during upload | Push tag during marketplace downtime | Step 8/9 timeout, retry logic kicks in, succeeds on 2nd attempt |
| TC-12: Duplicate release tag | Push same tag twice | Workflow runs twice, second run: Step 10 fails (release exists), but marketplaces already have version |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | `VS_MARKETPLACE_TOKEN` is valid Azure DevOps PAT with "Marketplace > Manage" scope | Not automatically verified; user must configure manually | Publishing fails at Step 8 |
| A2 | `OPEN_VSX_TOKEN` is valid Open VSX access token | Not automatically verified; user must configure manually | Publishing fails at Step 9 |
| A3 | VS Code Marketplace and Open VSX APIs accept multipart VSIX uploads | Verified: both use standard HTTP APIs | Upload fails if API format changes (e.g., registry update) |
| A4 | Publisher ID "chernistry" exists and is verified on both marketplaces | Not automatically verified; user must create accounts | Publishing fails with namespace/publisher error |
| A5 | GitHub Actions has write permissions to create releases | Verified: workflows/publish-extension.yml includes `permissions: contents: write` | Release creation fails at Step 10 |
| A6 | icon.png at path `packages/vscode/media/bernstein-icon.png` is always a valid PNG ≥128x128 | Not verified; relies on source control | Icon validation fails and blocks publish |
| A7 | package-lock.json is up-to-date and consistent with package.json | Not automatically verified; relies on developer discipline | npm ci fails with version conflict |

---

## Open Questions

- Should the workflow auto-bump the version in package.json after publishing, or leave that to the developer?
- What happens if an extension version is published to VS Code Marketplace but the Open VSX upload times out? Should we retry step 9 independently, or require a full re-tag?
- Should we generate release notes automatically from the CHANGELOG, or require manual entry in package.json?
- For Cursor forum verification (Step 11 below in UX spec), who posts the verification? Should this be automated or manual?

---

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-03-29 | Initial discovery: workflow already exists, well-structured | Documented as-is |
| — | Package.json has all required marketplace fields | Verified: displayName, description, icon, publisher, repository, homepage, license all present |
| — | Icon asset exists and is PNG | Verified: media/bernstein-icon.png exists and is valid PNG |
| — | Workflow uses HaaLeo/publish-vscode-extension@v1 action | Verified: action is correct, uses standard VS Code publishing API |

