# MCP Catalog

The Bernstein MCP catalog is a community registry of installable [Model Context Protocol](https://modelcontextprotocol.io) servers. It shipped in **release 1.9** and lives at `bernstein mcp catalog` (registered as a subgroup of `bernstein mcp` in `src/bernstein/cli/main.py`). Browse, search, install, upgrade, and uninstall MCP servers without hand-editing your client config.

The catalog is **fetched** (network call), **validated** against [`reference/mcp-catalog-schema.json`](mcp-catalog-schema.json), **cached** locally, and every state-changing call is **audited** through Bernstein's HMAC-chained audit log.

> Every flag below is cited as `src/bernstein/cli/commands/mcp_catalog_cmd.py:<line>`.

---

## Concepts

- **Catalog** - a JSON document listing MCP servers, their install commands, version pins, and verification status. Schema documented at [`reference/mcp-catalog-schema.json`](mcp-catalog-schema.json) (canonical) or below in [Schema example](#schema-example).
- **Entry** - one record in the catalog. Has an `id` (slug), `version_pin` (semver), `install_command` (argv), `verified_by_bernstein` (bool), `signature` (optional), and `transports` (list of stdio / http / sse).
- **Cache** - local JSON copy of the last-fetched catalog. Path resolved by `default_cache_path()`; overridable via `BERNSTEIN_MCP_CATALOG_CACHE_PATH`.
- **User MCP config** - the file Bernstein writes installed servers into. Path resolved by `default_user_config_path()`; overridable via `BERNSTEIN_MCP_USER_CONFIG_PATH`. Edits are bracketed by a "bernstein-managed" block so manual entries elsewhere in the file are preserved.
- **Audit log** - every fetch / install / upgrade / uninstall emits an HMAC-chained event under `.sdd/audit/`. Override directory via `BERNSTEIN_MCP_CATALOG_AUDIT_DIR` (`src/bernstein/cli/commands/mcp_catalog_cmd.py`).
- **Sandbox preview** - `install` and `upgrade` execute the entry's `install_command` in a sandbox **first**, capturing any file changes as a diff. Only after you confirm does Bernstein touch your real user config.

---

## `bernstein mcp catalog browse`

List every entry in the catalog.

**Synopsis:** `bernstein mcp catalog browse [flags]`

| Flag | Default | Meaning |
|---|---|---|
| `--refresh` | off | Skip the freshness window; force a fresh fetch. |

*(source: `src/bernstein/cli/commands/mcp_catalog_cmd.py`)*

The output is a Rich table with columns `ID`, `Name`, `Version`, `Verified`, `Transports`. The `Verified` column is `yes` if `verified_by_bernstein=true` in the catalog entry - i.e. Bernstein's trusted reviewers signed off on this manifest.

If validation fails (an entry has unknown fields, a missing required field, or a malformed signature), the **whole catalog fetch is rejected** and the cached copy is preserved. You will see `Catalog rejected: <reason>` and the previous catalog remains usable.

```bash
bernstein mcp catalog browse
bernstein mcp catalog browse --refresh
```

---

## `bernstein mcp catalog search <query>`

Search the catalog by ID, name, or description substring.

**Synopsis:** `bernstein mcp catalog search QUERY [flags]`

| Flag | Default | Meaning |
|---|---|---|
| `QUERY` | required | Substring (case-insensitive) matched against `id`, `name`, `description`. |
| `--refresh` | off | Skip the freshness window. |

*(source: `src/bernstein/cli/commands/mcp_catalog_cmd.py`)*

```bash
bernstein mcp catalog search github
bernstein mcp catalog search "vector store"
```

Output: one line per match, formatted `<id> (<version_pin>) <verified|unverified> - <name>: <description>`. Verified entries are green; unverified ones are yellow.

---

## `bernstein mcp catalog info <id>`

Show full details for a single entry.

**Synopsis:** `bernstein mcp catalog info ENTRY_ID [flags]`

| Flag | Default | Meaning |
|---|---|---|
| `ENTRY_ID` | required | The slug from `browse`/`search`. |
| `--refresh` | off | Skip the freshness window. |

*(source: `src/bernstein/cli/commands/mcp_catalog_cmd.py`)*

Prints the entry's name, version pin, description, homepage, repository, transports, verification status, auto-upgrade flag, install command, and signature (if any).

---

## `bernstein mcp catalog install <id>`

Install an MCP server into your user MCP config.

**Synopsis:** `bernstein mcp catalog install ENTRY_ID [flags]`

| Flag | Default | Meaning |
|---|---|---|
| `ENTRY_ID` | required | The slug to install. |
| `--yes` | off | Skip the confirmation prompt. |
| `--refresh` | off | Skip the freshness window. |

*(source: `src/bernstein/cli/commands/mcp_catalog_cmd.py`)*

**Flow:**

1. Bernstein fetches the catalog (or uses the cached copy if fresh).
2. If the entry is **unverified**, a yellow `WARNING` block is printed showing the exact `install_command` that would execute.
3. The install command is **executed in a sandbox first**. Bernstein records every file change, captures stdout/stderr, and renders an `InstallPreview` (succeeded / failed / timed-out, duration, file diff list).
4. If the sandbox preview fails, the install is aborted; **your user MCP config is left untouched**.
5. Otherwise Bernstein prompts: `Apply this install to the user MCP config?`. Use `--yes` to skip the prompt.
6. On confirmation, Bernstein writes the entry into the bernstein-managed block of the user MCP config.

The audit log records the install with a HMAC-chained event so the trail is tamper-evident.

```bash
bernstein mcp catalog install official-github
bernstein mcp catalog install some-experimental-server --yes  # skip prompt
```

---

## `bernstein mcp catalog list-installed`

List every entry currently installed via the catalog.

**Synopsis:** `bernstein mcp catalog list-installed`

*(source: `src/bernstein/cli/commands/mcp_catalog_cmd.py`)*

Output columns: `ID`, `Pinned` (the installed version), `Installed at` (timestamp), `Last upgrade check`, `In catalog` (`yes` if still listed in the current catalog, `no` if it was removed upstream).

A `no` in the `In catalog` column is a soft warning: the server still works, but you will not get upgrade notifications for it. Investigate before relying on it long-term.

---

## `bernstein mcp catalog upgrade [<id>]`

Re-fetch the catalog and upgrade installed entries.

**Synopsis:**
```
bernstein mcp catalog upgrade ENTRY_ID [flags]
bernstein mcp catalog upgrade --all  [flags]
```

| Flag | Default | Meaning |
|---|---|---|
| `ENTRY_ID` | optional | The slug to upgrade. Required unless `--all`. |
| `--all` | off | Upgrade every installed entry. |
| `--yes` | off | Skip confirmation prompts. |
| `--refresh` | off | Skip the freshness window. |

*(source: `src/bernstein/cli/commands/mcp_catalog_cmd.py`)*

For each upgrade, Bernstein:

1. Compares the installed `version_pin` against the catalog's current `version_pin`.
2. If equal, prints "already on latest" and skips.
3. Otherwise runs the same sandbox preview + confirmation flow as `install`.
4. Persists the new version pin into the user MCP config and the audit log.

Skipped upgrades print a `skipped_reason` (e.g. catalog rejection, sandbox failure).

```bash
bernstein mcp catalog upgrade official-github
bernstein mcp catalog upgrade --all --yes
```

---

## `bernstein mcp catalog uninstall <id>`

Remove an entry from the bernstein-managed block of the user MCP config.

**Synopsis:** `bernstein mcp catalog uninstall ENTRY_ID`

*(source: `src/bernstein/cli/commands/mcp_catalog_cmd.py`)*

Errors with `<id> is not installed` if the entry was never installed via the catalog. Manually-added user MCP entries are **not** affected - Bernstein only edits its own bracketed block.

```bash
bernstein mcp catalog uninstall some-experimental-server
```

---

## `bernstein mcp catalog status`

Show cache freshness, cadence settings, and installed-server count.

**Synopsis:** `bernstein mcp catalog status`

*(source: `src/bernstein/cli/commands/mcp_catalog_cmd.py`)*

Output keys:

- `Cache` - path to the cached catalog JSON.
- `Last fetch` - timestamp of the last successful fetch (or `never`).
- `Next due` - when the next background check is allowed.
- `Check interval (sec)` - how often Bernstein revalidates with the upstream registry. Tuned via `BERNSTEIN_MCP_CATALOG_CHECK_INTERVAL`.
- `Installed` - count of entries Bernstein has placed in the user MCP config.
- `Cache state` - last validation outcome (`ok`, `validation_error: ...`, etc.).

---

## Schema example

The full schema lives in [`reference/mcp-catalog-schema.json`](mcp-catalog-schema.json). A minimal valid entry:

```json
{
  "version": 1,
  "generated_at": "2026-04-30T12:00:00Z",
  "entries": [
    {
      "id": "official-github",
      "name": "GitHub MCP Server",
      "description": "Issue, PR, and repo browsing tools.",
      "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/github",
      "repository": "https://github.com/modelcontextprotocol/servers",
      "install_command": ["npx", "-y", "@modelcontextprotocol/server-github"],
      "version_pin": "0.4.2",
      "transports": ["stdio"],
      "verified_by_bernstein": true,
      "auto_upgrade": false,
      "signature": "sha256:..."
    }
  ]
}
```

Every field is required unless marked otherwise in the schema. `additionalProperties` is **`false`** at the top level **and** on each entry - the fetch is rejected wholesale if any unknown field is present. This is intentional: catalog drift would otherwise silently install servers with unrecognized capabilities.

---

## Configuration & environment variables

All five env vars come from `src/bernstein/cli/commands/mcp_catalog_cmd.py`.

| Env var | Default | Purpose |
|---|---|---|
| `BERNSTEIN_MCP_CATALOG_AUDIT_DIR` | `.sdd/audit/` | Where HMAC-chained audit events are written. |
| `BERNSTEIN_MCP_CATALOG_CHECK_INTERVAL` | `DEFAULT_CHECK_INTERVAL_SECONDS` (from `core/protocols/mcp_catalog`) | Minimum seconds between background catalog freshness checks. |
| `BERNSTEIN_MCP_CATALOG_REVALIDATE_INTERVAL` | `DEFAULT_REVALIDATE_SECONDS` | Minimum seconds before re-fetch of the upstream catalog. |
| `BERNSTEIN_MCP_CATALOG_CACHE_PATH` | `default_cache_path()` | Where the validated catalog JSON is cached on disk. |
| `BERNSTEIN_MCP_USER_CONFIG_PATH` | `default_user_config_path()` | The user MCP config file Bernstein writes installed entries into. |

All five accept absolute or `~`-expanded paths (where applicable) and parse integer overrides safely (a non-integer value is silently ignored and the default is kept).

---

## Trust model

- **`verified_by_bernstein`** - A boolean on each entry. `true` means a Bernstein-trusted reviewer has audited the upstream server's source and signed off on the install command and version pin combination as of this catalog generation. `false` is **not** a "blocked" mark - it just means you have not had a third party vouch for the server. Unverified entries trigger a yellow warning before install (`src/bernstein/cli/commands/mcp_catalog_cmd.py`).
- **`signature`** - Optional cryptographic signature over the entry. The schema permits any string; verification is delegated to the catalog service implementation in `core/protocols/mcp_catalog`.
- **`additionalProperties: false`** at top level and per-entry - the catalog is rejected wholesale on unknown fields. This blocks silent capability drift.
- **Sandbox preview** - Every install / upgrade runs the install command in a sandbox first. The host config is touched only after you confirm the diff. Failed previews abort without modifying state.
- **HMAC-chained audit** - Every fetch, install, upgrade, and uninstall is recorded to `.sdd/audit/` with a chained HMAC, making after-the-fact tampering detectable. The chain construction, verification, and recovery are documented once in [audit-log.md](../security/audit-log.md).
- **Bernstein-managed block** - Edits to the user MCP config are bracketed; Bernstein only owns its own block. Manually-added user entries elsewhere in the file are preserved.

There is no allow-list on which catalogs can be loaded; the catalog source URL is configured by the runtime, not by the user. If you operate in a high-trust environment, point the cache at an internally-mirrored catalog and treat the public catalog as untrusted by setting `BERNSTEIN_MCP_CATALOG_CACHE_PATH` and disabling auto-refresh.

---

## See also

- [`bernstein mcp`](cli-reference.md#bernstein-mcp) - root MCP server command (separate from catalog).
- [`integrations/mcp-server-injection.md`](../integrations/mcp-server-injection.md) - the `provide_mcp_servers` plugin hook for injecting servers from a plugin (different mechanism, different trust boundary).
- [Source schema](mcp-catalog-schema.json) - the canonical JSON Schema.
