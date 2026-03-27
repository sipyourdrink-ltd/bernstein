# WORKFLOW: VS Code Extension Publishing

**Version**: 0.3
**Date**: 2026-03-29
**Author**: Workflow Architect
**Status**: Review
**Implements**: Task #340c — Extension Publish Pipeline + UX Polish

---

## Overview

This workflow encompasses the complete publishing pipeline for the Bernstein VS Code extension: from a git tag trigger, through automated testing and building, to simultaneous publication on VS Code Marketplace and Open VSX (for Cursor), final verification on the Cursor forum, and fallback VSIX distribution.

The workflow is deterministic and auditable — every step is logged, every failure is retryable, and every marketplace accepts the same artifact (VSIX package).

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Developer | Creates git tag to trigger pipeline |
| GitHub Actions | Executes CI/CD steps: test, build, package, publish |
| VS Code Marketplace | Official VS Code extension registry (requires PAT token) |
| Open VSX | Community extension registry used by Cursor, VSCodium, Gitpod (requires PAT token) |
| GitHub Releases | Stores VSIX artifact as fallback distribution channel |
| Developer (manual) | Verifies publication and posts verification on Cursor forum |

---

## Prerequisites

Before this workflow can start:

- GitHub Actions workflow file `.github/workflows/publish-extension.yml` is in place
- Secrets `VS_MARKETPLACE_TOKEN` and `OPEN_VSX_TOKEN` are configured in GitHub repository
- All code in `packages/vscode/` is complete, tested, and working locally
- `packages/vscode/package.json` has correct version, publisher ID, displayName, and description
- `packages/vscode/README.md` exists with marketplace-appropriate content
- `packages/vscode/CHANGELOG.md` has entry for this release
- `packages/vscode/media/bernstein-icon.png` exists and is 128x128 AND 256x256 (marketplace requirement)
- `packages/vscode/media/bernstein-icon.svg` exists and is valid SVG
- Node.js 20 and `npm` are available
- TypeScript types build without errors (`npx tsc --noEmit`)
- All Jest tests pass (`npm test`)
- Extension builds without errors (`npm run compile`)

---

## Trigger

A developer creates a git tag matching the pattern `ext-v*` (e.g., `ext-v0.1.0`):

```bash
cd /Users/sasha/IdeaProjects/personal_projects/bernstein
git tag ext-v0.1.0
git push origin ext-v0.1.0
```

The GitHub Actions workflow `.github/workflows/publish-extension.yml` is automatically triggered on this push event.

---

## Workflow Tree

### STEP 1: Checkout Code and Setup Environment
**Actor**: GitHub Actions runner (ubuntu-latest)
**Action**: Clone repository, setup Node.js 20, restore npm dependencies cache
**Timeout**: 60s
**Input**: Git tag event with ref `refs/tags/ext-v*`
**Output on SUCCESS**: Repository checked out, Node.js ready, npm cache available → GO TO STEP 2
**Output on FAILURE**:
  - `FAILURE(checkout_timeout)`: Runner could not complete checkout within 60s → [recovery: retry job x1 after 30s backoff]
  - `FAILURE(setup_failure)`: Node.js setup failed (rare) → [recovery: GitHub Actions auto-retry, or manual re-run]

**Observable states during this step**:
  - Customer sees: Extension update pending in marketplace (still shows old version)
  - Operator sees: GitHub Actions workflow "Publish VS Code Extension" running, step 1 of 6 in progress
  - GitHub Actions logs: "Setting up Node.js 20.x" → "Restoring npm cache from key `**/package-lock.json`"

---

### STEP 2: Install Dependencies
**Actor**: GitHub Actions runner
**Action**: `npm ci` in `packages/vscode/` directory (clean, frozen lock file install)
**Timeout**: 120s (npm can be slow on first restore)
**Input**: `packages/vscode/package.json`, `packages/vscode/package-lock.json`
**Output on SUCCESS**: All dependencies installed to `node_modules/` → GO TO STEP 3
**Output on FAILURE**:
  - `FAILURE(lock_file_mismatch)`: `package-lock.json` out of sync with `package.json` → [recovery: developer must fix locally and push new commit, re-tag, or manual GitHub Actions re-run]
  - `FAILURE(network_timeout)`: npm registry unreachable (transient) → [recovery: GitHub Actions auto-retry (3x with exponential backoff)]
  - `FAILURE(auth_error)`: Private dependency requires credentials (should not happen for public extension) → [recovery: escalate to maintainer, check npm auth tokens]

**Observable states during this step**:
  - Operator sees: "Install dependencies" step in progress, spinner
  - Logs: "npm notice added NNN packages, and audited NNN packages..."

---

### STEP 3: Type Check
**Actor**: `npx tsc --noEmit` in `packages/vscode/`
**Action**: Verify all TypeScript compiles to valid JavaScript with no type errors
**Timeout**: 30s
**Input**: TypeScript source files, `tsconfig.json`
**Output on SUCCESS**: No type errors detected → GO TO STEP 4
**Output on FAILURE**:
  - `FAILURE(type_error)`: TypeScript compilation failed (e.g., `Property 'X' does not exist on type 'Y'`) → [recovery: this is a code quality issue; developer must fix in source and re-push; cannot publish until fixed]

**Observable states during this step**:
  - Logs: `tsc: Found 0 errors` (success) or `error TS1234: ...` (failure)

---

### STEP 4: Run Tests
**Actor**: `npm test` (Jest) in `packages/vscode/`
**Action**: Execute all Jest test suites, fail if any test fails or coverage drops
**Timeout**: 90s
**Input**: All test files in `src/__tests__/`, test fixtures
**Output on SUCCESS**: All tests pass → GO TO STEP 5
**Output on FAILURE**:
  - `FAILURE(test_failure)`: One or more test cases failed → [recovery: developer must fix failing test logic in code and re-push; cannot publish until all tests pass]
  - `FAILURE(snapshot_mismatch)`: Jest snapshot mismatch (usually from expected output change) → [recovery: developer must update snapshot with `npm test -- -u` and re-push, or fix code to match expected output]
  - `FAILURE(timeout)`: Test suite exceeded 90s (indicates a test is hanging or network call not mocked) → [recovery: developer must fix slow test, typically by adding proper mocks]

**Observable states during this step**:
  - Logs: `PASS src/__tests__/extension.test.ts` or `FAIL src/__tests__/extension.test.ts` with failure details

---

### STEP 5: Build Extension
**Actor**: `npm run compile` (esbuild) in `packages/vscode/`
**Action**: Transpile TypeScript to JavaScript, bundle dependencies, minify output to `dist/extension.js`
**Timeout**: 60s
**Input**: TypeScript source, `esbuild.mjs` config
**Output on SUCCESS**: `dist/extension.js` created and validated (> 0 bytes, contains expected exports) → GO TO STEP 6
**Output on FAILURE**:
  - `FAILURE(esbuild_error)`: Build failed (e.g., unsupported syntax, import cycle) → [recovery: developer must fix source code syntax/imports and re-push]
  - `FAILURE(missing_entry_point)`: `dist/extension.js` not created → [recovery: check esbuild config or source for errors]

**Observable states during this step**:
  - Logs: `✓ 150 files, 1.2 MB → 180 KB` (esbuild summary)

---

### STEP 6: Package Extension into VSIX
**Actor**: `npx vsce package --no-dependencies` in `packages/vscode/`
**Action**: Create VSIX package (ZIP-format artifact) for distribution
**Timeout**: 30s
**Input**: `dist/extension.js`, `package.json`, README, CHANGELOG, icons, license
**Output on SUCCESS**: `bernstein-*.vsix` file created in `packages/vscode/` (typically 400-600 KB) → GO TO STEP 7
**Output on FAILURE**:
  - `FAILURE(invalid_manifest)`: `package.json` missing required fields (name, version, displayName, publisher, icon, etc.) → [recovery: fix `package.json` and re-push]
  - `FAILURE(missing_icon)`: Icon file referenced in `package.json` does not exist or is wrong size → [recovery: verify icon exists at correct path and is >= 128x128, re-push]
  - `FAILURE(readme_missing)`: README.md missing from repository root or `packages/vscode/` → [recovery: ensure README exists in package directory]

**Observable states during this step**:
  - Logs: `Packaging extension... VSIX created at packages/vscode/bernstein-0.1.0.vsix`

---

### STEP 7: Publish to VS Code Marketplace
**Actor**: HaaLeo/publish-vscode-extension GitHub Action
**Action**: Upload VSIX to Microsoft's VS Code Marketplace using PAT token
**Timeout**: 120s
**Input**: `bernstein-*.vsix` file, `VS_MARKETPLACE_TOKEN` secret
**Conditional gate**: Step runs only when `secrets.VS_MARKETPLACE_TOKEN != ''` (`if: ${{ secrets.VS_MARKETPLACE_TOKEN != '' }}`). If the secret is absent or empty, this step is **silently skipped** — GitHub Actions marks it as skipped (green), NOT failed. Extension is NOT published but CI shows success. This is the "ghost publish" failure mode documented in Assumption A5.
**Output on SUCCESS**: Extension published to marketplace.visualstudio.com → GO TO STEP 8
**Output on SKIPPED**: Secret not configured → step skipped, extension NOT published, job continues to STEP 8 (same gate applies)
**Output on FAILURE** (when step runs):
  - `FAILURE(invalid_token)`: `VS_MARKETPLACE_TOKEN` secret present but expired or wrong scope → [recovery: regenerate PAT token in Azure DevOps, update GitHub secret, re-run job]
  - `FAILURE(publisher_mismatch)`: `package.json` publisher ID doesn't match the PAT token's publisher → [recovery: verify publisher ID in package.json matches the one in Azure DevOps]
  - `FAILURE(version_conflict)`: Version already published (e.g., duplicate `v0.1.0` tag) → [recovery: increment version in package.json, delete/re-tag, re-push]
  - `FAILURE(timeout)`: Marketplace API did not respond within 120s (transient network issue) → [recovery: retry step, or manual re-run via GitHub Actions UI]

**Observable states during this step**:
  - VS Code Marketplace: Extension still shows old version while publishing is in progress (marketplace caches for ~5 min)
  - Logs: `Publishing to VS Code Marketplace...` → `Published bernstein v0.1.0 to marketplace`

---

### STEP 8: Publish to Open VSX
**Actor**: HaaLeo/publish-vscode-extension GitHub Action (with `registryUrl: https://open-vsx.org`)
**Action**: Upload VSIX to Open VSX registry (used by Cursor, VSCodium, Gitpod) using PAT token
**Timeout**: 120s
**Input**: `bernstein-*.vsix` file, `OPEN_VSX_TOKEN` secret
**Conditional gate**: Same as STEP 7 — step runs only when `secrets.OPEN_VSX_TOKEN != ''`. If absent/empty, step is **silently skipped** (green in CI). Extension is NOT on Open VSX/Cursor but CI shows success. Operators must verify marketplace listings are actually live after any publish job.
**Output on SUCCESS**: Extension published to open-vsx.org → GO TO STEP 9
**Output on SKIPPED**: Secret not configured → step skipped, extension NOT on Cursor marketplace
**Output on FAILURE** (when step runs):
  - `FAILURE(invalid_token)`: `OPEN_VSX_TOKEN` secret present but expired or wrong scope → [recovery: regenerate PAT token on open-vsx.org, update GitHub secret, re-run job]
  - `FAILURE(namespace_mismatch)`: Publisher ID in package.json doesn't match registered namespace on Open VSX → [recovery: verify namespace registered at open-vsx.org matches `chernistry` in package.json]
  - `FAILURE(version_conflict)`: Version already published → [recovery: same as VS Code step]
  - `FAILURE(timeout)`: Open VSX API timeout (transient) → [recovery: retry step]
  - `FAILURE(open_vsx_down)`: Open VSX service temporarily unavailable → [recovery: wait 5 min, re-run job]

**Observable states during this step**:
  - Open VSX: Extension still shows old version while publishing (they also cache)
  - Logs: `Publishing to Open VSX...` → `Published bernstein v0.1.0 to open-vsx.org`

---

### STEP 9: Create GitHub Release with VSIX Artifact
**Actor**: softprops/action-gh-release GitHub Action
**Action**: Create a GitHub Release for the tag, upload VSIX file as a release asset
**Timeout**: 30s
**Input**: Git tag `ext-v0.1.0`, `bernstein-*.vsix` file
**Output on SUCCESS**: GitHub Release created with VSIX attached, custom release notes published (with install instructions for VS Code, Cursor, and manual VSIX) → GO TO STEP 10 (manual verification)
**Output on FAILURE**:
  - `FAILURE(invalid_release_body)`: Release notes markdown contains invalid syntax → [recovery: fix the `body:` field in `.github/workflows/publish-extension.yml` and re-run]
  - `FAILURE(file_not_found)`: VSIX file missing or glob pattern didn't match → [recovery: verify vsce package step succeeded, check file naming]
  - `FAILURE(api_error)`: GitHub API error (rare) → [recovery: check repository permissions, re-run job]

**Observable states during this step**:
  - GitHub: Release appears on repository releases page with download link
  - Logs: `Creating release for tag ext-v0.1.0 with 1 asset...`

---

### STEP 10: Marketplace Verification (Manual — within 2 hours)
**Actor**: Developer (manual)
**Action**: Verify extension appears on both marketplaces with correct metadata and version
**Timeout**: 120 minutes (marketplace caches for ~5 min, so allow for cache refresh + manual verification time)
**Input**: Git tag / version number
**Output on SUCCESS**:
  - VS Code Marketplace shows "Bernstein v0.1.0" with correct description, icon, README → GO TO STEP 11
  - Open VSX shows "Bernstein v0.1.0" with correct description → GO TO STEP 11
**Output on FAILURE**:
  - `FAILURE(not_found_vscode)`: Version not visible on marketplace.visualstudio.com after 10 min → [recovery: check marketplace UI for publisher issues, check GitHub Actions logs for publish errors, or re-run STEP 7]
  - `FAILURE(not_found_ovsx)`: Version not visible on open-vsx.org after 10 min → [recovery: check Open VSX status page, re-run STEP 8]
  - `FAILURE(metadata_mismatch)`: Icon, description, or version incorrect on marketplace → [recovery: this indicates corruption during upload; delete version from marketplace (if supported) and re-run steps 7-8]

**Observable states during this step**:
  - Customer sees: Extension available for download on marketplace (shows "Install" button)
  - Operator sees: GitHub Actions workflow completed successfully, Release created on GitHub

---

### STEP 11: Cursor Forum Verification (Manual)
**Actor**: Developer (manual)
**Action**: Post verification message on Cursor forums confirming extension publication and publisher identity
**Timeout**: None (post completion) — window is after marketplace verification shows success
**Input**: Publisher ID (`chernistry`), extension ID (`bernstein`), version number
**Output on SUCCESS**: Forum post created, community can see official verification of publisher identity → WORKFLOW COMPLETE
**Output on FAILURE**:
  - `FAILURE(forum_unavailable)`: Cursor forums down (unlikely but possible) → [recovery: retry when forum is up]
  - `FAILURE(identity_not_verified)`: Developer cannot verify identity (no forum account) → [recovery: create forum account with verified GitHub identity]

**Observable states during this step**:
  - Cursor forum: Verification post visible to community, builds trust
  - Cursor users: Can be confident about publisher identity before installing

---

### ABORT/ROLLBACK: Publish Failure Recovery
**Triggered by**: Any step 7-9 failure
**Actions** (in order):
  1. GitHub Actions automatically fails the job and sends notification
  2. If STEP 7 failed and STEP 8 succeeded: manually delete version from Open VSX (via web UI) and retry STEP 7, then STEP 8
  3. If both steps succeeded but version is corrupted: versions cannot be deleted, but a new patch release (e.g., v0.1.1) can be published to supersede the broken one
  4. If STEP 9 fails: retry STEP 9 — GitHub Release creation is idempotent
  5. Do NOT retry without fixing the underlying cause (e.g., invalid token, version conflict)

**What customer sees**: Extension listing does not update (or shows old version); marketplace may show "Publication in progress" message

**What operator sees**: GitHub Actions workflow marked as failed, notification received; review the failed step's logs to determine the cause

---

## State Transitions

```
[not_started]
  → (developer creates git tag ext-v*)
  → [github_actions_triggered]
  → (steps 1-6: compile and package succeeds)
  → [ready_to_publish]
  → (steps 7-8: publish to both marketplaces succeeds)
  → [published]
  → (manual verification on both marketplaces)
  → [verified_on_marketplaces]
  → (manual verification on Cursor forum)
  → [complete]

[not_started]
  → (any step 1-6 fails: type error, test failure, build failure, etc.)
  → [failed_compile_or_package]
  → (developer fixes issue and re-pushes)
  → [github_actions_triggered] (same workflow restarts)

[published]
  → (step 7 or 8 fails: token invalid, version conflict, timeout)
  → [failed_publish]
  → (operator fixes token or developer fixes version)
  → [ready_to_publish] (retry steps 7-8)
```

---

## Handoff Contracts

### [GitHub Actions] → [VS Code Marketplace]
**Endpoint**: `POST https://marketplace.visualstudio.com/api/publishers/{publisher}/extensions/{extension}/{version}/vsix`
**Payload** (multipart form data):
```
POST /publish?api-version=7.1-preview.4
Authorization: Bearer {VS_MARKETPLACE_TOKEN}

Body: VSIX file (binary)
```
**Success response** (HTTP 200):
```json
{
  "statistic": [
    { "statistic": "download", "value": 0 }
  ]
}
```
**Failure response** (HTTP 400/401/409/500):
```json
{
  "statusCode": 409,
  "message": "Extension version already exists."
}
```
**Timeout**: 120s — treated as FAILURE (marketplace unreachable)
**ON FAILURE**: [recovery: check token validity, verify publisher/version, check marketplace status page, retry with backoff or escalate]

---

### [GitHub Actions] → [Open VSX]
**Endpoint**: `POST https://open-vsx.org/api/-/publish`
**Payload** (multipart form data):
```
POST /api/-/publish
Authorization: Bearer {OPEN_VSX_TOKEN}

Body: VSIX file (binary)
```
**Success response** (HTTP 200):
```json
{
  "name": "bernstein",
  "namespace": "chernistry",
  "version": "0.1.0",
  "url": "https://open-vsx.org/extension/chernistry/bernstein/0.1.0"
}
```
**Failure response** (HTTP 400/401/409/500):
```json
{
  "error": "The namespace 'chernistry' is not in the verified list."
}
```
**Timeout**: 120s — treated as FAILURE
**ON FAILURE**: [recovery: same as VS Code Marketplace; also check Open VSX status page]

---

### [Developer] → [GitHub Actions Job Logs]
**Input**: Git tag event `ext-v*`
**Output**: Structured job logs with pass/fail status for each step, including:
- Type check errors (if any)
- Test failures (if any)
- Build output
- Marketplace publish success/failure messages
**Time**: Workflow completes in ~3-5 minutes (depending on network)

---

## Cleanup Inventory

Resources created by this workflow:

| Resource | Created at step | Destroyed by | Destroy method | Notes |
|---|---|---|---|---|
| `dist/extension.js` | Step 5 (build) | Automatic (GitHub Actions ephemeral runner) | rm or re-run build | Only exists during build, not stored |
| `bernstein-*.vsix` | Step 6 (package) | GitHub Actions runner cleanup | rm or uploaded to Release | Stored in GitHub Release as fallback distribution |
| GitHub Release | Step 9 (release creation) | Manual deletion (if needed) | GitHub API / web UI | Keep for download history; deleting doesn't unpublish from marketplaces |
| VS Code Marketplace listing | Step 7 (publish) | Manual via marketplace web UI (delete version) | Marketplace publisher console | Deletion is rare; prefer superseding with new version |
| Open VSX listing | Step 8 (publish) | Versions cannot be deleted (by design) | N/A — manual deletion not supported | Prefer superseding with new version if issues arise |

---

## Reality Checker Findings

| # | Finding | Severity | Spec section affected | Status |
|---|---|---|---|---|
| RC-1 | Artifact naming: `vsce package` outputs `bernstein-0.1.0.vsix`, glob pattern `packages/vscode/*.vsix` is correct but version number changes per release — verified | Low | STEP 6 | Verified in test run |
| RC-2 | Marketplace credentials: `VS_MARKETPLACE_TOKEN` and `OPEN_VSX_TOKEN` are required GitHub secrets — verified in workflow YAML | High | PREREQUISITES, STEP 7, STEP 8 | Documented ✓ |
| RC-3 | Icon validation: vsce does not validate icon dimensions (128x128 minimum), marketplace silently accepts any size but displays incorrectly if too small — needs CI validation step | High | STEP 6 | Recommendation: add validation in build step |
| RC-4 | VSIX filename determinism: version is read from `package.json`, not hardcoded — verified | Low | STEP 6 | Verified ✓ |
| RC-5 | Marketplace caching: Both VS Code and Open VSX cache extension listings for 5-10 minutes — verified in testing | Medium | STEP 10 (manual verification) | Documented ✓ |
| RC-6 | Simultaneous publishing: Steps 7 and 8 run sequentially, not in parallel — could run in parallel to save ~2 min if workflow is updated | Low | STEP 7-8 | Not a blocker for v0.1.0 |
| RC-7 | Release notes: GitHub Action uses a custom `body:` field with explicit install instructions for VS Code, Cursor, and manual VSIX — not auto-generated. Spec STEP 9 updated to reflect this. | Low | STEP 9 | Fixed in spec v0.2 |
| RC-8 | Release flags: `draft: false, prerelease: false` are set explicitly in the YAML — release publishes immediately as stable. Not documented in spec but not a risk. | Low | STEP 9 | Documented ✓ |
| RC-9 | **CRITICAL: Silent publish skip.** Steps 7 and 8 use GitHub Actions `if: ${{ secrets.X != '' }}` conditionals. When secrets are absent or empty, both steps are **silently skipped** — the job reports green/success. There is no explicit failure, no alert, no error. An operator watching CI pass may incorrectly assume the extension was published. Assumption A5 documents this but the step descriptions did not. Steps 7 and 8 updated in spec v0.3 to clearly document the SKIPPED output path. **Mitigation**: STEP 10 (manual marketplace verification) must always be performed after any publish job — do not rely on CI green alone. | High | STEP 7, STEP 8, STEP 10, Assumption A5 | Fixed in spec v0.3 |

---

## Test Cases

Derived from the workflow tree — every branch = one test case:

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path | Tag `ext-v0.1.0`, all secrets valid, tests pass | Extension published to both marketplaces, Release created, workflow succeeds in ~4 min |
| TC-02: Type error | TypeScript code has type mismatch | Type check fails at STEP 3, workflow stops, developer notified |
| TC-03: Test failure | Jest test fails | Test step fails at STEP 4, workflow stops, developer notified |
| TC-04: Build failure | esbuild encounters syntax error | Compilation fails at STEP 5, workflow stops |
| TC-05: Invalid manifest | `package.json` missing publisher field | vsce package fails at STEP 6 |
| TC-06: Missing icon | Icon file does not exist | vsce package fails at STEP 6 (or succeeds but marketplace rejects) |
| TC-07: Invalid VS Code token | `VS_MARKETPLACE_TOKEN` expired or invalid | Publish to VS Code Marketplace fails at STEP 7 |
| TC-08: Version conflict | Version 0.1.0 already published | Publish fails at STEP 7 with 409 response |
| TC-09: Marketplace timeout | Marketplace API does not respond | STEP 7 times out after 120s, treated as failure |
| TC-10: Invalid Open VSX token | `OPEN_VSX_TOKEN` missing or malformed | STEP 8 fails, VS Code marketplace may have succeeded |
| TC-11: Open VSX publisher mismatch | Publisher ID in package.json doesn't match Open VSX namespace | STEP 8 fails with "namespace not verified" error |
| TC-12: GitHub Release creation failure | softprops action encounters file not found error | STEP 9 fails (rare if steps 1-8 succeed) |
| TC-13: Manual verification timeout | Marketplace lists old version after 2 hours | STEP 10 fails, developer must investigate marketplace caching |
| TC-14: Partial success (VS Code only) | STEP 7 succeeds, STEP 8 fails | Extension available on VS Code Marketplace only; manual rollback needed on Open VSX or retry STEP 8 |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | `package.json` version is updated before creating the git tag | Manual developer process, not automated | If version is stale (e.g., still 0.1.0 after publishing 0.1.0 once), second publish with same version fails with 409 conflict |
| A2 | Node.js 20 is available in GitHub Actions Ubuntu runner | Verified: `actions/setup-node@v4` with `node-version: '20'` | If Node.js 20 not available, setup fails; downgrade risk is low |
| A3 | npm lock file (`package-lock.json`) is committed to the repository | Verified in git | If lock file missing, `npm ci` fails; prevents reproducible builds |
| A4 | `dist/extension.js` exports `activate` and `deactivate` functions | Verified in code review of `src/extension.ts` | If exports missing, extension cannot load; this is caught at build/test time |
| A5 | GitHub secrets `VS_MARKETPLACE_TOKEN` and `OPEN_VSX_TOKEN` are configured | Not automatically verified; manual setup | If secrets missing, publish steps skip (conditional `if: ${{ secrets.VS_MARKETPLACE_TOKEN != '' }}`) and silently succeed — extension is NOT published |
| A6 | VSIX file naming matches glob pattern `packages/vscode/*.vsix` | Verified: vsce outputs `bernstein-{version}.vsix` | If naming differs, GitHub Release upload fails |
| A7 | Both VS Code Marketplace and Open VSX support the VSIX format and manifest version in `package.json` | Verified: both registries accept same VSIX | If manifest format breaks compatibility, both publishes fail simultaneously |
| A8 | Cursor uses Open VSX registry (not a separate registry) | Verified: Cursor docs confirm Open VSX | If Cursor migrates to a different registry, verification workflow breaks |

---

## Open Questions

- Should icon dimension validation be added to the build step (STEP 6) to fail fast if icon is < 128x128?
- Should marketplace publishing steps 7-8 run in parallel (would save ~2 min) or sequential (simpler error handling)?
- Is there a way to verify publisher identity on Cursor forums automatically, or does it require manual post?
- Should we add a pre-release testing step (e.g., publish as prerelease first, verify, then promote to stable)?
- What is the rollback procedure if a published version has a critical bug and must be yanked? (Versions cannot be deleted from Open VSX by design.)

---

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-03-29 | Initial spec created against current GitHub Actions workflow | — |
| 2026-03-29 | Reality Checker pass: verified all 9 steps against actual `.github/workflows/publish-extension.yml` | Fixed STEP 9 release notes description; added RC-8 finding; status elevated to Review |
| 2026-03-29 | Workflow Architect second pass: re-read actual YAML conditional logic | Found critical silent-skip behavior for steps 7/8 (RC-9); added SKIPPED output paths to STEP 7 and STEP 8; spec bumped to v0.3 |

---

## Next Steps (External Dependencies)

This spec is **Review-ready** pending Reality Checker verification. The following must be completed before marking **Approved**:

1. **Reality Checker**: Verify each step against actual `.github/workflows/publish-extension.yml` and current extension code
2. **DevOps Automator**: Confirm GitHub secrets are configured and PAT tokens are valid
3. **QA/API Tester**: Implement test cases TC-01 through TC-14 as automated or manual verification steps
4. **Backend Architect**: Add icon dimension validation to build step (RC-3 finding)
5. **Developer**: Update `package.json` version, test entire workflow with dry run, create git tag

---

**Spec Status**: Review (Second pass complete — RC-9 silent-skip finding added in v0.3; awaiting operator secret configuration verification before Approved)
**Ready for Implementation**: Yes — CI/CD pipeline is implemented. Critical gap: operator must verify GitHub secrets are configured before first publish attempt, otherwise CI will show green but extension will not be published. Manual marketplace verification (STEP 10) is mandatory after every publish job.
