# WORKFLOW: Plugin Marketplace with Versioned, Signed, and Reviewed Community Plugins

**Version**: 1.0
**Date**: 2026-04-11
**Author**: Workflow Architect
**Status**: Draft
**Implements**: road-089 — Plugin marketplace with versioned, signed, and reviewed community plugins

---

## Overview

A community plugin marketplace where users publish, discover, install, update, and uninstall quality gates, adapters, role templates, plan templates, hooks, and MCP server bundles. Each plugin carries a validated manifest, semver version, cryptographic signature, review status, install count, and compatibility matrix. The CLI surface is `bernstein plugin install|update|uninstall|search|publish|info <name>`. Distinct from MCP-014 (MCP server marketplace in `mcp_marketplace.py`) — this covers **all** plugin types through a unified registry.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Plugin Author | Develops, signs, and publishes plugins to the marketplace |
| Marketplace Index | Central registry file (`.sdd/config/marketplace-index.json`) listing all available plugins with metadata |
| CLI (`bernstein plugin`) | User-facing commands for search, install, update, uninstall, publish, info |
| `plugin_installer.py` | Fetches and extracts plugin artifacts from sources (GitHub, git, npm, file, directory) |
| `plugin_manifest.py` | Validates plugin manifest against schema + anti-impersonation rules |
| `plugin_trust.py` | Computes trust score and risk level from signature, tests, README, pyproject |
| `plugin_policy.py` | Enforces enterprise allowlist/blocklist before installation |
| `plugin_reconciler.py` | Auto-uninstalls delisted plugins on startup |
| `plugin_errors.py` | Collects and surfaces plugin errors |
| `PluginManager` | Loads and registers installed plugins via pluggy hookspecs |
| Plugin Reviewer (human or bot) | Reviews submitted plugins, approves/rejects, sets review status |
| Operator | Manages enterprise policy, monitors plugin health |

---

## Prerequisites

- `.bernstein/plugins/` directory exists (or will be created on first install)
- `.sdd/config/marketplace-index.json` is accessible (fetched from remote or bundled)
- For publishing: plugin author has a signing key pair (Ed25519)
- For enterprise: `.bernstein/plugins-policy.yaml` may restrict installable plugins
- Network access to GitHub/npm/git for remote sources
- `bernstein` CLI installed and functional

---

## Trigger

Multiple entry points:

| Trigger | Actor | Entry point |
|---|---|---|
| `bernstein plugin search <query>` | User | CLI search command |
| `bernstein plugin install <name>[@version]` | User | CLI install command |
| `bernstein plugin update [name]` | User | CLI update command |
| `bernstein plugin uninstall <name>` | User | CLI uninstall command |
| `bernstein plugin publish <path>` | Plugin Author | CLI publish command |
| `bernstein plugin info <name>` | User | CLI info command |
| Orchestrator startup | System | `reconcile_plugins()` auto-uninstall pass |
| Plugin load on `bernstein run` | System | `PluginManager.load_from_workdir()` |

---

## Sub-Workflows

This spec covers six distinct sub-workflows:

1. **Plugin Search & Discovery** — browsing the marketplace index
2. **Plugin Install** — downloading, validating, trust-checking, policy-checking, installing
3. **Plugin Update** — version comparison, upgrade-in-place with rollback
4. **Plugin Uninstall** — removal with cleanup
5. **Plugin Publish** — packaging, signing, submitting to marketplace
6. **Startup Reconciliation** — auto-uninstall delisted, validate installed

---

## Sub-Workflow 1: Plugin Search & Discovery

### Trigger
`bernstein plugin search <query>` or `bernstein plugin list`

### STEP 1.1: Fetch Marketplace Index
**Actor**: CLI
**Action**: Read `.sdd/config/marketplace-index.json`. If stale (>24h since last fetch), pull fresh index from configured remote URL.
**Timeout**: 15s for remote fetch
**Input**: `{ query: string, plugin_type?: string, min_review_status?: string }`
**Output on SUCCESS**: `{ entries: MarketplacePluginEntry[] }` → GO TO STEP 1.2
**Output on FAILURE**:
  - `FAILURE(network_timeout)`: Remote unreachable → fall back to cached index, warn user "Using cached index (last updated {date})"
  - `FAILURE(no_cache)`: No cached index and remote unreachable → return error "No marketplace index available. Check network."
  - `FAILURE(parse_error)`: Index file malformed → return error "Marketplace index corrupted. Run `bernstein plugin refresh`."

**Observable states**:
  - Customer sees: "Fetching marketplace index..." or immediate results from cache
  - Logs: `[marketplace] index fetch started source={url}`

### STEP 1.2: Filter and Rank Results
**Actor**: CLI
**Action**: Filter entries by query match (name, description, keywords), plugin_type, review_status. Rank by: exact name match > keyword match > description match. Secondary sort by install_count descending.
**Input**: `{ entries: MarketplacePluginEntry[], query: string, filters: object }`
**Output on SUCCESS**: `{ results: MarketplacePluginEntry[], total: int }` → display table to user

**Observable states**:
  - Customer sees: Table with columns: Name, Version, Type, Review Status, Installs, Trust Level, Description
  - Installed plugins marked with checkmark; updatable plugins marked with arrow

---

## Sub-Workflow 2: Plugin Install

### Trigger
`bernstein plugin install <name>[@version]`

### STEP 2.1: Resolve Plugin from Index
**Actor**: CLI
**Action**: Look up `<name>` in marketplace index. If `@version` specified, find that exact version. If no version, use latest stable (highest semver with review_status != "rejected").
**Timeout**: 5s (index is local or cached)
**Input**: `{ name: string, version?: string }`
**Output on SUCCESS**: `{ entry: MarketplacePluginEntry, source: PluginSource }` → GO TO STEP 2.2
**Output on FAILURE**:
  - `FAILURE(not_found)`: Plugin name not in index → return error "Plugin '{name}' not found. Run `bernstein plugin search` to browse."
  - `FAILURE(version_not_found)`: Plugin exists but requested version does not → return error "Version {version} not found for '{name}'. Available: {versions}"
  - `FAILURE(rejected)`: Plugin review_status is "rejected" → return error "Plugin '{name}' has been rejected and cannot be installed."

**Observable states**:
  - Customer sees: "Resolving {name}..."
  - Logs: `[marketplace] resolving plugin={name} version={version}`

### STEP 2.2: Check Enterprise Policy
**Actor**: `plugin_policy.py`
**Action**: Load `.bernstein/plugins-policy.yaml`. Call `check_plugin_allowed(name, policy)`.
**Input**: `{ plugin_name: string, policy: PluginPolicy }`
**Output on SUCCESS**: Policy permits installation → GO TO STEP 2.3
**Output on FAILURE**:
  - `FAILURE(blocklisted)`: Plugin is on blocklist → return error "Plugin '{name}' is blocked by enterprise policy. Contact your administrator."
  - `FAILURE(not_on_allowlist)`: Allowlist is non-empty and plugin not on it → return error "Plugin '{name}' is not on the enterprise allowlist. Contact your administrator."

**Observable states**:
  - Customer sees: "Checking policy..."
  - Logs: `[policy] plugin={name} result={allowed|blocked}`

### STEP 2.3: Check Already Installed
**Actor**: CLI
**Action**: Check if `.bernstein/plugins/{name}/` exists. If so, read installed version from `manifest.yaml`. If installed version == requested version, abort with "already installed". If different version, prompt user: "Plugin '{name}' v{installed} is already installed. Upgrade to v{requested}? [y/N]"
**Input**: `{ name: string, requested_version: string, install_dir: Path }`
**Output on SUCCESS (not installed)**: → GO TO STEP 2.4
**Output on SUCCESS (upgrade confirmed)**: → GO TO Sub-Workflow 3 (Plugin Update)
**Output on FAILURE**:
  - `FAILURE(already_installed)`: Same version → return "Plugin '{name}' v{version} is already installed."
  - `FAILURE(upgrade_declined)`: User declined upgrade → return "Installation cancelled."

### STEP 2.4: Download Plugin Artifact
**Actor**: `plugin_installer.py`
**Action**: Fetch artifact from the resolved source (GitHub release, git clone, npm pack, etc.). Extract to a temporary directory.
**Timeout**: 120s (large artifacts, slow networks)
**Input**: `{ source: PluginSource, temp_dir: Path }`
**Output on SUCCESS**: `{ extracted_path: Path }` → GO TO STEP 2.5
**Output on FAILURE**:
  - `FAILURE(network_timeout)`: Download exceeded 120s → return error "Download timed out. Check network and try again."
  - `FAILURE(network_error)`: URLError, connection refused → return error "Failed to download plugin: {error}"
  - `FAILURE(archive_corrupt)`: BadZipFile, TarError → return error "Downloaded artifact is corrupt. The release may be damaged."
  - `FAILURE(npm_error)`: npm pack failed → return error "npm packaging failed: {error}"

**Observable states**:
  - Customer sees: "Downloading {name} v{version}..." with progress if available
  - Logs: `[installer] downloading source={source_type} plugin={name}`

### STEP 2.5: Validate Manifest
**Actor**: `plugin_manifest.py`
**Action**: Load `manifest.yaml` from extracted directory. Call `validate_manifest()`. Verify name matches marketplace entry name (anti-impersonation). Verify version matches requested version.
**Input**: `{ extracted_path: Path, expected_name: string, expected_version: string }`
**Output on SUCCESS**: `{ manifest: PluginManifest }` → GO TO STEP 2.6
**Output on FAILURE**:
  - `FAILURE(no_manifest)`: No `manifest.yaml` in plugin root → return error "Plugin has no manifest.yaml. Not a valid Bernstein plugin."
  - `FAILURE(validation_error)`: Schema violations → return error "Manifest validation failed:\n{errors}"
  - `FAILURE(name_mismatch)`: Manifest name ≠ marketplace entry name → return error "SECURITY: Manifest name '{manifest_name}' does not match expected '{expected_name}'. Possible impersonation. Installation blocked."
  - `FAILURE(version_mismatch)`: Manifest version ≠ requested version → return error "Version mismatch: manifest says {manifest_ver}, marketplace says {expected_ver}."
  - `FAILURE(blocked_prefix)`: Name uses reserved prefix (bernstein-, anthropic-, openai-, claude-code-) → return error "Plugin name uses reserved prefix."

**Observable states**:
  - Customer sees: "Validating manifest..."
  - Logs: `[manifest] validating plugin={name} version={version}`

### STEP 2.6: Verify Signature
**Actor**: `plugin_trust.py` (extended)
**Action**: If `.signature` file is present, verify it cryptographically against the plugin contents using the author's public key (stored in marketplace index). If no signature and plugin is from a non-verified author, warn user.
**Input**: `{ extracted_path: Path, author_public_key?: string }`
**Output on SUCCESS (signed + valid)**: `{ signature_valid: true, signed_by: string }` → GO TO STEP 2.7
**Output on SUCCESS (unsigned)**: `{ signature_valid: false, reason: "unsigned" }` → warn user, prompt "Install unsigned plugin? [y/N]" → if yes, GO TO STEP 2.7
**Output on FAILURE**:
  - `FAILURE(signature_invalid)`: Cryptographic verification failed → return error "SECURITY: Signature verification failed. The plugin may have been tampered with. Installation blocked."
  - `FAILURE(key_not_found)`: Author public key not in marketplace index → warn "Author's signing key not registered. Cannot verify signature."

**Observable states**:
  - Customer sees: "Verifying signature..." or "WARNING: Plugin is unsigned."
  - Logs: `[trust] signature_check plugin={name} result={valid|invalid|unsigned}`

### STEP 2.7: Compute Trust Score
**Actor**: `plugin_trust.py`
**Action**: Run full trust assessment: signature, source_verified, has_tests, has_readme, has_pyproject. Compute risk_level and trust_score.
**Input**: `{ extracted_path: Path }`
**Output on SUCCESS**: `{ trust: PluginTrust }` → GO TO STEP 2.8
  - If risk_level == "unknown": warn user "This plugin has no trust signals. Proceed with caution. [y/N]"

**Observable states**:
  - Customer sees: Trust summary — "Trust: {score}/100 ({risk_level}) — Signed: {y/n}, Tests: {y/n}, README: {y/n}"
  - Logs: `[trust] plugin={name} score={score} risk={risk_level}`

### STEP 2.8: Check Compatibility
**Actor**: CLI
**Action**: Read `compatibility` field from manifest: `{ min_bernstein_version: string, max_bernstein_version?: string, python_version?: string, plugin_types: string[] }`. Compare against running Bernstein version.
**Input**: `{ manifest: PluginManifest, current_version: string }`
**Output on SUCCESS**: Compatible → GO TO STEP 2.9
**Output on FAILURE**:
  - `FAILURE(incompatible_version)`: Bernstein version outside range → return error "Plugin requires Bernstein {min}..{max}, you have {current}."
  - `FAILURE(incompatible_python)`: Python version mismatch → return error "Plugin requires Python {required}, you have {current}."

### STEP 2.9: Install to Plugin Directory
**Actor**: `plugin_installer.py`
**Action**: Move extracted plugin from temp dir to `.bernstein/plugins/{name}/`. Write `meta.json` with: `{ name, version, installed_at, source, signature_valid, trust_score, risk_level, review_status }`.
**Input**: `{ extracted_path: Path, install_dir: Path, metadata: object }`
**Output on SUCCESS**: `{ install_path: Path }` → GO TO STEP 2.10
**Output on FAILURE**:
  - `FAILURE(permission_error)`: Cannot write to plugins dir → return error "Permission denied writing to {install_dir}."
  - `FAILURE(disk_full)`: ENOSPC → return error "Disk full. Cannot install plugin."

**Observable states**:
  - Customer sees: "Installing to .bernstein/plugins/{name}/..."
  - Database: `meta.json` created in plugin directory
  - Logs: `[installer] installed plugin={name} version={version} path={path}`

### STEP 2.10: Update Install Count
**Actor**: CLI
**Action**: If connected to remote marketplace, POST install event to increment install_count. Best-effort — failure does not block.
**Timeout**: 5s
**Input**: `{ plugin_name: string, version: string }`
**Output on SUCCESS**: Count incremented → DONE
**Output on FAILURE**: Silently log and continue — install is already complete.

**Observable states**:
  - Customer sees: "Installed {name} v{version} (trust: {score}/100, {risk_level})"
  - Logs: `[marketplace] install_count_update plugin={name} result={ok|failed}`

---

## Sub-Workflow 3: Plugin Update

### Trigger
`bernstein plugin update [name]` — if no name, update all installed plugins.

### STEP 3.1: Identify Updatable Plugins
**Actor**: CLI
**Action**: For each installed plugin (or specified plugin), compare installed version (from `meta.json`) against latest version in marketplace index.
**Input**: `{ name?: string, install_dir: Path }`
**Output on SUCCESS**: `{ updates: [{ name, installed_version, available_version }] }` → GO TO STEP 3.2
**Output on SUCCESS (no updates)**: → return "All plugins are up to date."
**Output on FAILURE**:
  - `FAILURE(no_meta)`: Installed plugin has no `meta.json` → warn "Plugin '{name}' has no metadata. Reinstall with `bernstein plugin install {name}` to enable updates."

### STEP 3.2: Backup Current Installation
**Actor**: CLI
**Action**: Copy `.bernstein/plugins/{name}/` to `.bernstein/plugins/.backup/{name}-{version}/`. This enables rollback if the update fails.
**Input**: `{ name: string, install_path: Path }`
**Output on SUCCESS**: `{ backup_path: Path }` → GO TO STEP 3.3
**Output on FAILURE**:
  - `FAILURE(disk_full)`: Cannot create backup → abort update, return error "Cannot backup current installation — disk full."

**Observable states**:
  - Customer sees: "Backing up {name} v{installed}..."
  - Logs: `[update] backup created plugin={name} path={backup_path}`

### STEP 3.3: Install New Version
**Actor**: Delegates to Sub-Workflow 2 (STEP 2.4 through 2.9)
**Action**: Download, validate, verify, install new version. The existing plugin directory is replaced.
**Output on SUCCESS**: → GO TO STEP 3.4
**Output on FAILURE**: → ROLLBACK

### STEP 3.4: Verify Plugin Loads
**Actor**: `PluginManager`
**Action**: Attempt to load the updated plugin in isolation. If it fails to load (import error, hookspec mismatch), rollback.
**Input**: `{ plugin_path: Path }`
**Output on SUCCESS**: Plugin loads → GO TO STEP 3.5
**Output on FAILURE**:
  - `FAILURE(import_error)`: Plugin fails to import → ROLLBACK
  - `FAILURE(hookspec_mismatch)`: Plugin implements hooks that don't exist in this version → ROLLBACK

### STEP 3.5: Remove Backup
**Actor**: CLI
**Action**: Delete `.bernstein/plugins/.backup/{name}-{version}/`. Update complete.
**Input**: `{ backup_path: Path }`
**Output on SUCCESS**: → DONE

**Observable states**:
  - Customer sees: "Updated {name} v{old} → v{new}"
  - Logs: `[update] complete plugin={name} old={old_ver} new={new_ver}`

### ROLLBACK
**Triggered by**: STEP 3.3 or 3.4 failure
**Actions**:
  1. Remove failed installation at `.bernstein/plugins/{name}/`
  2. Restore backup from `.bernstein/plugins/.backup/{name}-{version}/` to `.bernstein/plugins/{name}/`
  3. Remove backup directory
**What customer sees**: "Update failed: {error}. Rolled back to v{installed}."
**What operator sees**: Plugin remains at previous version.
**Logs**: `[update] rollback plugin={name} restored_version={version}`

---

## Sub-Workflow 4: Plugin Uninstall

### Trigger
`bernstein plugin uninstall <name>`

### STEP 4.1: Verify Plugin Exists
**Actor**: CLI
**Action**: Check `.bernstein/plugins/{name}/` exists.
**Output on FAILURE**: `FAILURE(not_installed)` → return error "Plugin '{name}' is not installed."

### STEP 4.2: Check Active Usage
**Actor**: `PluginManager`
**Action**: Check if plugin is currently loaded and active in a running session. If `bernstein run` is active, warn user.
**Output on SUCCESS (not active)**: → GO TO STEP 4.3
**Output on SUCCESS (active)**: Warn "Plugin is in use by a running session. Uninstall after session ends? [y/N]"

### STEP 4.3: Remove Plugin Directory
**Actor**: CLI
**Action**: `shutil.rmtree(.bernstein/plugins/{name}/)`. Remove entry from any local state (meta.json references, etc.).
**Output on SUCCESS**: → DONE
**Output on FAILURE**:
  - `FAILURE(permission_error)`: → return error "Permission denied. Cannot remove plugin directory."
  - `FAILURE(os_error)`: → return error "Failed to remove plugin: {error}"

**Observable states**:
  - Customer sees: "Uninstalled {name}."
  - Logs: `[uninstall] removed plugin={name}`

---

## Sub-Workflow 5: Plugin Publish

### Trigger
`bernstein plugin publish <path>` — path to plugin directory with `manifest.yaml`

### STEP 5.1: Validate Manifest
**Actor**: `plugin_manifest.py`
**Action**: Load and validate `manifest.yaml` from the plugin directory. All fields must be present and valid.
**Input**: `{ plugin_path: Path }`
**Output on SUCCESS**: `{ manifest: PluginManifest }` → GO TO STEP 5.2
**Output on FAILURE**: Same as STEP 2.5 failures.

### STEP 5.2: Run Plugin Tests
**Actor**: CLI
**Action**: If `tests/` directory exists, run `pytest tests/ -x -q` in the plugin directory. Test pass is required for publish.
**Timeout**: 300s
**Input**: `{ plugin_path: Path }`
**Output on SUCCESS**: All tests pass → GO TO STEP 5.3
**Output on FAILURE**:
  - `FAILURE(tests_failed)`: → return error "Plugin tests failed. Fix tests before publishing.\n{output}"
  - `FAILURE(timeout)`: → return error "Plugin tests exceeded 5 minute timeout."
  - `FAILURE(no_tests)`: Warn "No tests found. Plugin will have lower trust score." → GO TO STEP 5.3

### STEP 5.3: Sign Plugin
**Actor**: Plugin Author
**Action**: If author has a signing key configured (`~/.bernstein/signing-key.pem`), compute Ed25519 signature over plugin contents (hash of all files excluding `.signature`). Write `.signature` file.
**Input**: `{ plugin_path: Path, signing_key_path?: Path }`
**Output on SUCCESS (signed)**: `{ signature_file: Path, fingerprint: string }` → GO TO STEP 5.4
**Output on SUCCESS (no key)**: Warn "No signing key found. Plugin will be published unsigned. Generate one with `bernstein plugin keygen`." → GO TO STEP 5.4

**Observable states**:
  - Customer sees: "Signing plugin..." or "WARNING: Publishing unsigned."
  - Logs: `[publish] signed plugin={name} fingerprint={fp}`

### STEP 5.4: Package Plugin
**Actor**: CLI
**Action**: Create `.tar.gz` archive of plugin directory. Include: manifest.yaml, all source files, tests/, README, .signature (if present). Exclude: __pycache__, .git, .env, node_modules.
**Input**: `{ plugin_path: Path }`
**Output on SUCCESS**: `{ archive_path: Path, sha256: string }` → GO TO STEP 5.5

### STEP 5.5: Submit to Marketplace
**Actor**: CLI
**Action**: Upload archive to marketplace (GitHub release on a designated repo, or a POST to marketplace API). Create marketplace entry with metadata: name, version, description, author, plugin_types, compatibility, sha256, signature_fingerprint.
**Timeout**: 60s
**Input**: `{ archive_path: Path, manifest: PluginManifest, sha256: string }`
**Output on SUCCESS**: `{ entry_url: string, review_status: "pending_review" }` → DONE
**Output on FAILURE**:
  - `FAILURE(name_conflict)`: Plugin name already taken by another author → return error "Plugin name '{name}' is already registered by another author."
  - `FAILURE(network_error)`: → return error "Failed to submit: {error}"
  - `FAILURE(auth_error)`: Missing or invalid marketplace credentials → return error "Authentication failed. Run `bernstein plugin login` first."

**Observable states**:
  - Customer sees: "Published {name} v{version}. Status: pending_review. URL: {url}"
  - Logs: `[publish] submitted plugin={name} version={version} status=pending_review`

---

## Sub-Workflow 6: Startup Reconciliation

### Trigger
`bernstein run` startup — called from `PluginManager.load_from_workdir()`

### STEP 6.1: Reconcile Against Marketplace
**Actor**: `plugin_reconciler.py`
**Action**: Call `reconcile_plugins()`. For each installed plugin, check if it still exists in marketplace index. Delisted plugins are auto-uninstalled.
**Input**: `{ plugins_dir: Path, marketplace_path: Path }`
**Output on SUCCESS**: `{ removed: string[], kept: string[], errors: string[] }` → GO TO STEP 6.2

### STEP 6.2: Validate All Installed Manifests
**Actor**: `plugin_manifest.py`
**Action**: For each remaining installed plugin, load and validate `manifest.yaml`. Report invalid plugins.
**Output**: List of valid plugins and list of invalid plugins with errors → GO TO STEP 6.3

### STEP 6.3: Check Enterprise Policy
**Actor**: `plugin_policy.py`
**Action**: For each valid plugin, check against enterprise policy. Skip blocked plugins.
**Output**: List of policy-permitted plugins → GO TO STEP 6.4

### STEP 6.4: Compute Trust for All
**Actor**: `plugin_trust.py`
**Action**: Compute trust score for each permitted plugin. Log warnings for "unknown" risk plugins.
**Output**: Trust report → continue to normal PluginManager load

**Observable states**:
  - Customer sees: Summary — "Loaded {n} plugins. {m} removed (delisted). {k} skipped (policy/validation)."
  - Logs: `[startup] reconciliation complete loaded={n} removed={m} skipped={k}`

---

## State Transitions

```
[not_installed] -> (install succeeds) -> [installed]
[installed] -> (update succeeds) -> [installed] (new version)
[installed] -> (update fails) -> [installed] (rollback to old version)
[installed] -> (uninstall) -> [not_installed]
[installed] -> (reconcile: delisted) -> [not_installed]
[installed] -> (policy: blocked) -> [installed but skipped at load]

Plugin review lifecycle:
[submitted] -> (reviewer approves) -> [approved]
[submitted] -> (reviewer rejects) -> [rejected]
[approved] -> (new version submitted) -> [submitted] (new version)
[approved] -> (delisted by author) -> [delisted]
[approved] -> (delisted by admin) -> [delisted]
```

---

## Handoff Contracts

### CLI → `plugin_installer.install_plugin()`
**Input**: `PluginSource` (one of GitHub/Git/Npm/File/Directory), `install_dir: Path`
**Success response**: `PluginInstallResult(success=True, path=..., message=...)`
**Failure response**: `PluginInstallResult(success=False, error="...", path=None)`
**Timeout**: 120s

### CLI → `plugin_manifest.validate_manifest()`
**Input**: `dict` (parsed YAML)
**Success response**: `PluginManifest` dataclass
**Failure response**: raises `ManifestValidationError(errors: list[str])`
**Timeout**: N/A (CPU-bound, <1s)

### CLI → `plugin_trust.check_plugin_trust()`
**Input**: `plugin_path: Path`
**Success response**: `PluginTrust` dataclass with score, risk_level, signals
**Failure response**: raises `FileNotFoundError`
**Timeout**: N/A (filesystem only, <1s)

### CLI → `plugin_policy.check_plugin_allowed()`
**Input**: `plugin_name: str, policy: PluginPolicy`
**Success response**: returns None (allowed)
**Failure response**: raises `PluginPolicyViolation(message: str)`
**Timeout**: N/A (in-memory, <1ms)

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| Temp download directory | STEP 2.4 | STEP 2.9 (on success) or cleanup (on failure) | `shutil.rmtree(temp_dir)` |
| Extracted plugin in temp | STEP 2.4 | STEP 2.9 (moved) or cleanup (on failure) | `shutil.rmtree(temp_dir)` |
| Plugin directory | STEP 2.9 | Uninstall or reconcile | `shutil.rmtree(plugin_dir)` |
| meta.json | STEP 2.9 | Uninstall or reconcile | Deleted with plugin directory |
| Backup directory | STEP 3.2 | STEP 3.5 (success) or ROLLBACK (restored) | `shutil.rmtree(backup_dir)` |
| Archive file | STEP 5.4 | After STEP 5.5 completes | `os.unlink(archive_path)` |

---

## MarketplacePluginEntry Schema

```json
{
  "name": "string — unique plugin identifier, validated against name rules",
  "version": "string — semver (e.g. 1.2.3)",
  "description": "string — one-line description",
  "author": "string — author name or org",
  "author_public_key": "string? — Ed25519 public key for signature verification",
  "plugin_types": ["string — quality_gate | adapter | role_template | plan_template | hook | mcp_server"],
  "compatibility": {
    "min_bernstein_version": "string — semver",
    "max_bernstein_version": "string? — semver",
    "python_version": "string? — PEP 440 specifier"
  },
  "source": {
    "type": "github | git | npm",
    "repo": "string? — for github",
    "url": "string? — for git",
    "package": "string? — for npm",
    "tag": "string? — git tag or GitHub release tag"
  },
  "sha256": "string — hash of the published archive",
  "signature_fingerprint": "string? — fingerprint of the .signature file",
  "review_status": "pending_review | approved | rejected",
  "install_count": "int",
  "published_at": "string — ISO 8601",
  "updated_at": "string — ISO 8601"
}
```

---

## Reality Checker Findings

| # | Finding | Severity | Spec section affected | Resolution |
|---|---|---|---|---|
| RC-1 | `install_plugin()` does not call `validate_manifest()` | Critical | Sub-Workflow 2, STEP 2.5 | Must wire manifest validation into install pipeline |
| RC-2 | `check_plugin_trust()` is not called during install or load | Critical | Sub-Workflow 2, STEP 2.7 | Must wire trust check into install pipeline |
| RC-3 | `.signature` check is presence-only, no cryptographic verification | Critical | Sub-Workflow 2, STEP 2.6 | Must implement Ed25519 verification against author public key |
| RC-4 | `reconcile_plugins()` is never called in production code | High | Sub-Workflow 6, STEP 6.1 | Must wire into `PluginManager.load_from_workdir()` startup |
| RC-5 | `install_plugin()` does not write `meta.json` | High | Sub-Workflow 2, STEP 2.9 | CLI `plugins list` reads `meta.json` but installer never writes it |
| RC-6 | `update_plugin()` is identical to `install_plugin()` — no version compare, no rollback | High | Sub-Workflow 3 | Must add backup/rollback logic |
| RC-7 | `report_plugin_error()` is not called from PluginManager discover/load paths | Medium | Sub-Workflow 6 | Should replace `warnings.warn` with `report_plugin_error` |
| RC-8 | `bernstein.triggers` and `bernstein.reporters` entry-point groups have no discovery code | Medium | N/A (related gap) | Dead entry-point groups in pyproject.toml |
| RC-9 | No `bernstein plugin` CLI command group exists yet | High | All sub-workflows | Must implement CLI command group |
| RC-10 | Marketplace index is local-only (`.sdd/config/marketplace-index.json` does not exist yet) | High | Sub-Workflow 1 | Must define index format, fetch mechanism, and seed data |

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path install | `bernstein plugin install my-gate@1.0.0` | Downloads, validates manifest, checks trust, installs to `.bernstein/plugins/my-gate/`, writes `meta.json` |
| TC-02: Install blocked by policy | Plugin on blocklist | Returns policy violation error, no files created |
| TC-03: Install unsigned plugin | Plugin has no `.signature` | Warns user, prompts for confirmation |
| TC-04: Install with invalid signature | `.signature` fails verification | Returns security error, blocks install |
| TC-05: Install incompatible version | Plugin requires Bernstein >99.0 | Returns version incompatibility error |
| TC-06: Install name impersonation | Manifest name ≠ marketplace name | Returns security error, blocks install |
| TC-07: Update with rollback | New version fails to load (import error) | Restores backup, reports error |
| TC-08: Update already latest | Installed version == available version | Reports "already up to date" |
| TC-09: Uninstall active plugin | Plugin in use by running session | Warns user, asks for confirmation |
| TC-10: Publish unsigned | No signing key configured | Warns, publishes with review_status=pending |
| TC-11: Publish with tests failing | Tests fail in plugin directory | Blocks publish, shows test output |
| TC-12: Startup reconciliation | Delisted plugin installed | Auto-removes delisted plugin, loads remaining |
| TC-13: Search with filters | `bernstein plugin search gate --type quality_gate` | Returns filtered, ranked results |
| TC-14: Network timeout on index fetch | Remote unreachable | Falls back to cached index with warning |
| TC-15: Duplicate name publish | Name taken by another author | Returns name conflict error |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | Ed25519 is the signing algorithm | Not verified — no crypto code exists yet | Must choose algorithm before implementation |
| A2 | Marketplace index is a JSON file, not a database | Design decision | Scales to ~10k plugins; beyond that needs a service |
| A3 | Plugin directory layout is `.bernstein/plugins/{name}/` | Verified: `plugin_reconciler.py` uses this convention | Low |
| A4 | `manifest.yaml` is the canonical manifest filename | Verified: `load_plugin_manifest()` accepts any path | Must standardize on YAML |
| A5 | Remote marketplace is a GitHub repo with releases | Not verified — no remote exists yet | Could be any HTTP endpoint |
| A6 | Install count tracking is best-effort (no transaction) | Design decision | Counts may undercount; acceptable |
| A7 | Plugin types match the six categories listed | Partially verified: adapters, gates, hooks have entry-point groups | `role_template` and `plan_template` types are new |

## Open Questions

- What is the remote marketplace backend? GitHub repo releases? Dedicated API service? Static JSON on CDN?
- Should there be a `bernstein plugin keygen` command or delegate to existing GPG/SSH key infrastructure?
- What is the review process? Human reviewers? Automated checks? Both?
- Should plugins be sandboxed at runtime (separate process, restricted filesystem)?
- How are plugin author identities verified? GitHub account? Email? Organization membership?
- Should the marketplace support private/enterprise-only plugins?

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-11 | Initial spec created. 10 Reality Checker findings documented. | — |
